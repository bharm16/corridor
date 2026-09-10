"""The Work Decision service seam: assign an Internal Owner (ADR-0025).

A Work Decision proves only what the project decided and when. These tests
drive the one seam every later Work Decision surface consumes: principal +
Dependency + state change in, typed immutable receipt out, current value
projected, audit holding a pointer and never the payload.
"""

import json
from datetime import date

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError

from corridor.audit import ASSIGN_INTERNAL_OWNER, DEPENDENCY, trail_for_commitment_lineage
from corridor.models import (
    AuditLog,
    CommitmentLineage,
    Dependency,
    DependencyEvent,
    DocPage,
    Document,
    ExternalOrg,
    Milestone,
    Project,
    WorkDecision,
    WorkDecisionMilestoneImpact,
)
from corridor.principals import HumanPrincipal, InvalidHumanPrincipal
from corridor.statement_lifecycle import observe_current_statement
from corridor.work_decisions import (
    CoordinationDecisionRefusal,
    CoordinationSubject,
    FOLLOW_UP_NEXT_ACTION_CHOICES,
    StaleNextAction,
    assign_internal_owner,
    cancel_next_action,
    complete_next_action,
    current_deferral_decision,
    current_internal_owner_decision,
    current_milestone_impact_decision,
    current_next_action_decision,
    defer_work,
    set_milestone_impact,
    set_next_action,
)

RECORDER = HumanPrincipal("local:coordination-recorder")


@pytest.fixture
def dependency(session, project):
    d = Dependency(
        project_id=project.id,
        ref_code="WD-1",
        dep_type="utility_relocation",
        title="Water main at 1102+20",
    )
    session.add(d)
    session.flush()
    return d


@pytest.fixture
def accepted_statement(session, project, dependency):
    """A real attributable Commitment that may own a Coordination Plan."""
    from corridor.external_statements import (
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    party = ExternalOrg(name="Work Decisions Party")
    session.add(party)
    session.flush()
    dependency.external_org_id = party.id
    session.flush()
    event = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party=party.name,
        stated_external_org_id=party.id,
        source_kind="verbal",
        event_date=date(2026, 8, 12),
        description="Work Decisions Party will provide the relocation schedule.",
        new_timing=StatementTiming.day("August 20, 2026", date(2026, 8, 20)),
        scope=StatementScope.selected((dependency.id,)),
        created_by="local:statement-coordinator",
    )
    return event


@pytest.fixture
def accepted_date_change(session, project, dependency):
    """An accepted date change is the only statement that carries impact."""
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    party = ExternalOrg(name="Date Change Party")
    session.add(party)
    session.flush()
    dependency.external_org_id = party.id
    document = Document(
        project_id=project.id,
        sha256="d" * 64,
        filename="date-change-minutes.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    quote = "Date Change Party moves completion from March to May 16."
    session.add(DocPage(document_id=document.id, page_no=1, text=quote))
    session.flush()
    return record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party=party.name,
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=None,
        description=quote,
        previous_timing=StatementTiming.month("March 2026", 2026, 3),
        new_timing=StatementTiming.day("May 16, 2026", date(2026, 5, 16)),
        scope=StatementScope.selected((dependency.id,)),
        created_by="local:statement-coordinator",
        evidence=CitedStatementEvidence(document.id, 1, quote),
    )


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


def test_a_commitment_lineage_can_own_an_independent_internal_owner_chain(
    session, accepted_statement
):
    """The plan follows the accepted statement, never its scope Dependency."""
    subject = CoordinationSubject.statement(
        accepted_statement.commitment_lineage_id
    )

    decision = assign_internal_owner(
        session, subject, "Dana Fields", principal=RECORDER
    )

    lineage = session.get(CommitmentLineage, subject.commitment_lineage_id)
    assert decision.dependency_id is None
    assert decision.commitment_lineage_id == lineage.id
    assert lineage.internal_owner == "Dana Fields"


def test_current_statement_observation_returns_the_fact_a_plan_answers(
    session, accepted_date_change
):
    subject = CoordinationSubject.statement(
        accepted_date_change.commitment_lineage_id
    )
    impact = set_milestone_impact(
        session,
        subject,
        "does_not_affect",
        principal=RECORDER,
    )

    observation = observe_current_statement(
        session, accepted_date_change.commitment_lineage_id
    )

    assert observation is not None
    assert observation.event.id == accepted_date_change.id
    assert observation.scope_decision.scope_mode == "selected"
    assert observation.milestone_impact_decision.id == impact.id


def test_current_statement_observation_moves_forward_with_the_fact(
    session, accepted_date_change, dependency
):
    from corridor.external_statements import (
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    subject = CoordinationSubject.statement(
        accepted_date_change.commitment_lineage_id
    )
    set_milestone_impact(
        session,
        subject,
        "does_not_affect",
        principal=RECORDER,
    )
    successor = record_external_party_statement(
        session,
        project_id=accepted_date_change.project_id,
        affected_external_org_id=accepted_date_change.affected_external_org_id,
        stated_party=accepted_date_change.stated_party,
        stated_external_org_id=accepted_date_change.stated_external_org_id,
        source_kind="verbal",
        event_date=date(2026, 8, 13),
        description="Date Change Party confirms one current completion date.",
        new_timing=StatementTiming.day("May 16, 2026", date(2026, 5, 16)),
        scope=StatementScope.selected((dependency.id,)),
        created_by="local:statement-coordinator",
        commitment_lineage_id=accepted_date_change.commitment_lineage_id,
    )

    observation = observe_current_statement(
        session, accepted_date_change.commitment_lineage_id
    )

    assert observation is not None
    assert observation.event.id == successor.id
    assert observation.scope_decision.event_id == successor.id
    assert observation.milestone_impact_decision is None


def test_the_database_refuses_neither_or_both_coordination_subjects(
    session, dependency, accepted_statement
):
    """XOR lives below the public service seam as well as above it."""
    for values in (
        {},
        {
            "dependency_id": dependency.id,
            "commitment_lineage_id": accepted_statement.commitment_lineage_id,
        },
    ):
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    WorkDecision(
                        **values,
                        decision_type="assign_internal_owner",
                        field="internal_owner",
                        before_value=None,
                        after_value="Dana Fields",
                        recorded_by=RECORDER.subject,
                    )
                )
                session.flush()


def test_statement_plan_remains_on_its_lineage_after_a_factual_successor(
    session, accepted_statement, dependency
):
    """A timing correction reviews one plan; scope does not copy it to a row."""
    from corridor.external_statements import (
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    subject = CoordinationSubject.statement(
        accepted_statement.commitment_lineage_id
    )
    assign_internal_owner(session, subject, "Dana Fields", principal=RECORDER)
    successor = record_external_party_statement(
        session,
        project_id=accepted_statement.project_id,
        affected_external_org_id=accepted_statement.affected_external_org_id,
        stated_party="Work Decisions Party",
        stated_external_org_id=accepted_statement.stated_external_org_id,
        source_kind="verbal",
        event_date=date(2026, 8, 13),
        description="Work Decisions Party will provide the revised schedule.",
        new_timing=StatementTiming.day("August 21, 2026", date(2026, 8, 21)),
        scope=StatementScope.selected((dependency.id,)),
        created_by="local:statement-coordinator",
        commitment_lineage_id=subject.commitment_lineage_id,
    )

    lineage = session.get(CommitmentLineage, subject.commitment_lineage_id)
    session.refresh(dependency)
    assert successor.commitment_lineage_id == subject.commitment_lineage_id
    assert successor.supersedes_event_id == accepted_statement.id
    assert lineage.plan_needs_review is True
    assert lineage.internal_owner == "Dana Fields"
    assert dependency.internal_owner is None


def test_scope_correction_never_copies_a_statement_plan_to_a_dependency(
    session, accepted_statement, dependency
):
    """Scope names applicability, never an implicit Dependency plan."""
    from corridor.external_statements import (
        StatementScope,
        record_statement_scope_decision,
    )

    subject = CoordinationSubject.statement(
        accepted_statement.commitment_lineage_id
    )
    assign_internal_owner(session, subject, "Dana Fields", principal=RECORDER)
    record_statement_scope_decision(
        session,
        event_id=accepted_statement.id,
        scope=StatementScope.selected((dependency.id,)),
        actor=RECORDER,
    )

    lineage = session.get(CommitmentLineage, subject.commitment_lineage_id)
    assert lineage.internal_owner == "Dana Fields"
    assert lineage.plan_needs_review is False
    assert _decisions(session, dependency.id) == []
    assert dependency.internal_owner is None


def test_statement_action_projection_refuses_a_tampered_chain_tail(
    session, accepted_statement
):
    subject = CoordinationSubject.statement(
        accepted_statement.commitment_lineage_id
    )
    set_next_action(
        session,
        subject,
        "Call the party",
        due_date=date(2026, 8, 20),
        principal=RECORDER,
    )
    lineage = session.get(CommitmentLineage, subject.commitment_lineage_id)
    lineage.next_action = "Tampered"
    session.flush()

    with pytest.raises(ValueError, match="diverged"):
        set_next_action(
            session,
            subject,
            "Send the confirmation",
            due_date=date(2026, 8, 21),
            principal=RECORDER,
        )


def test_statement_action_reason_projection_refuses_a_tampered_chain_tail(
    session, accepted_statement
):
    subject = CoordinationSubject.statement(
        accepted_statement.commitment_lineage_id
    )
    set_next_action(
        session,
        subject,
        "Call the party",
        due_date_unknown_reason="awaiting_external_information",
        principal=RECORDER,
    )
    lineage = session.get(CommitmentLineage, subject.commitment_lineage_id)
    lineage.action_due_date_reason = "awaiting_schedule_information"
    session.flush()

    with pytest.raises(ValueError, match="diverged"):
        complete_next_action(
            session,
            subject,
            no_follow_up_reason="return_condition_recorded",
            principal=RECORDER,
        )


def test_date_change_milestone_impact_has_its_own_chain_and_exact_links(
    session, project, accepted_date_change
):
    subject = CoordinationSubject.statement(
        accepted_date_change.commitment_lineage_id
    )
    milestone = Milestone(
        project_id=project.id,
        code="UTILITY-READY",
        name="Utility ready for construction",
        need_date=date(2026, 9, 1),
    )
    session.add(milestone)
    session.flush()

    decision = set_milestone_impact(
        session,
        subject,
        "affects",
        milestone_ids=(milestone.id,),
        principal=RECORDER,
    )

    assert decision.field == "milestone_impact"
    assert json.loads(decision.after_value) == {
        "milestone_ids": [milestone.id],
        "state": "affects",
    }
    assert current_milestone_impact_decision(session, subject).id == decision.id
    assert session.scalars(
        select(WorkDecisionMilestoneImpact).where(
            WorkDecisionMilestoneImpact.work_decision_id == decision.id
        )
    ).one().milestone_id == milestone.id


def test_date_change_successor_as_commitment_hides_historical_milestone_impact(
    session, project, dependency, accepted_date_change
):
    """A re-derived Commitment does not retain a current date-change impact."""
    from corridor.external_statements import (
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    subject = CoordinationSubject.statement(
        accepted_date_change.commitment_lineage_id
    )
    milestone = Milestone(
        project_id=project.id,
        code="DATE-CHANGE-IMPACT",
        name="Date change impact",
        need_date=date(2026, 9, 1),
    )
    session.add(milestone)
    session.flush()
    set_milestone_impact(
        session,
        subject,
        "affects",
        milestone_ids=(milestone.id,),
        principal=RECORDER,
    )

    successor = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=accepted_date_change.affected_external_org_id,
        stated_party="Date Change Party",
        stated_external_org_id=accepted_date_change.stated_external_org_id,
        source_kind="verbal",
        event_date=date(2026, 8, 13),
        description="Date Change Party now commits to May 16.",
        new_timing=StatementTiming.day("May 16, 2026", date(2026, 5, 16)),
        scope=StatementScope.selected((dependency.id,)),
        created_by="local:statement-coordinator",
        commitment_lineage_id=subject.commitment_lineage_id,
    )

    lineage = session.get(CommitmentLineage, subject.commitment_lineage_id)
    assert successor.event_type == "commitment"
    assert lineage.plan_needs_review is True
    assert current_milestone_impact_decision(session, subject) is None


def test_unknown_milestone_impact_remains_coordinated_work(
    session, accepted_date_change
):
    subject = CoordinationSubject.statement(
        accepted_date_change.commitment_lineage_id
    )
    with pytest.raises(ValueError, match="owner, action"):
        set_milestone_impact(
            session, subject, "not_yet_known", principal=RECORDER
        )

    assign_internal_owner(session, subject, "Dana Fields", principal=RECORDER)
    set_next_action(
        session,
        subject,
        "Assess milestone impact",
        due_date_unknown_reason="awaiting_schedule_information",
        principal=RECORDER,
    )
    decision = set_milestone_impact(
        session, subject, "not_yet_known", principal=RECORDER
    )

    assert json.loads(decision.after_value)["state"] == "not_yet_known"
    # The trigger is deferred so the receipt and its exact links may be
    # appended atomically.  Force it here: empty links are valid for the
    # explicit unknown state, not an accidental missing-link exception.
    session.execute(text("set constraints all immediate"))


def test_database_refuses_unknown_milestone_impact_without_current_plan(
    session, accepted_date_change
):
    """Direct SQL cannot make unresolved Impact look like coordinated work."""
    with pytest.raises(IntegrityError, match="Internal Owner and Next Action"):
        with session.begin_nested():
            session.add(
                WorkDecision(
                    commitment_lineage_id=accepted_date_change.commitment_lineage_id,
                    decision_type="set_milestone_impact",
                    field="milestone_impact",
                    before_value=None,
                    after_value=json.dumps(
                        {"milestone_ids": [], "state": "not_yet_known"},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    recorded_by=RECORDER.subject,
                )
            )
            session.flush()
            session.execute(text("set constraints all immediate"))


def test_database_refuses_a_milestone_from_another_project(
    session, accepted_date_change
):
    """Exact impact links cannot cross the statement subject's project."""
    other_project = Project(
        slug="other-impact-project", name="Other impact project", is_synthetic=True
    )
    session.add(other_project)
    session.flush()
    foreign_milestone = Milestone(
        project_id=other_project.id,
        code="OTHER-MILESTONE",
        name="A milestone in another project",
        need_date=date(2026, 9, 1),
    )
    session.add(foreign_milestone)
    session.flush()

    with pytest.raises(IntegrityError, match="cannot cross projects"):
        with session.begin_nested():
            decision = WorkDecision(
                commitment_lineage_id=accepted_date_change.commitment_lineage_id,
                decision_type="set_milestone_impact",
                field="milestone_impact",
                before_value=None,
                after_value=json.dumps(
                    {"milestone_ids": [foreign_milestone.id], "state": "affects"},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                recorded_by=RECORDER.subject,
            )
            session.add(decision)
            session.flush()
            session.add(
                WorkDecisionMilestoneImpact(
                    work_decision_id=decision.id,
                    milestone_id=foreign_milestone.id,
                )
            )
            session.flush()
            session.execute(text("set constraints all immediate"))


def test_action_closure_records_a_structured_reason_or_successor(
    session, accepted_statement
):
    subject = CoordinationSubject.statement(
        accepted_statement.commitment_lineage_id
    )
    set_next_action(
        session,
        subject,
        "Call the party",
        due_date_unknown_reason="awaiting_external_information",
        principal=RECORDER,
    )
    completed = complete_next_action(
        session,
        subject,
        no_follow_up_reason="return_condition_recorded",
        note="Return when the schedule arrives.",
        principal=RECORDER,
    )

    assert completed.no_follow_up_reason == "return_condition_recorded"
    assert completed.note == "Return when the schedule arrives."
    assert current_next_action_decision(session, subject).after_value is None

    set_next_action(
        session,
        subject,
        "Check the estimate",
        due_date_unknown_reason="awaiting_external_information",
        principal=RECORDER,
    )
    cancelled = cancel_next_action(
        session,
        subject,
        successor_action="Confirm the revised estimate",
        successor_due_date=date(2026, 8, 22),
        cancellation_reason="superseded",
        no_follow_up_reason=None,
        principal=RECORDER,
    )
    current = current_next_action_decision(session, subject)

    assert cancelled.cancellation_reason == "superseded"
    assert cancelled.no_follow_up_reason is None
    assert json.loads(current.after_value)["action"] == "Confirm the revised estimate"


def test_action_closure_refuses_to_invent_structured_reasons(
    session, accepted_statement
):
    subject = CoordinationSubject.statement(
        accepted_statement.commitment_lineage_id
    )
    set_next_action(
        session,
        subject,
        "Call the party",
        due_date=date(2026, 8, 20),
        principal=RECORDER,
    )

    with pytest.raises(ValueError, match="no-follow-up reason"):
        complete_next_action(session, subject, principal=RECORDER)
    with pytest.raises(ValueError, match="no-follow-up reason"):
        cancel_next_action(session, subject, principal=RECORDER)


def test_close_binds_to_the_exact_action_the_coordinator_saw(
    session, accepted_statement
):
    """A stale expected id refuses without closing a different action (#334)."""
    subject = CoordinationSubject.statement(
        accepted_statement.commitment_lineage_id
    )
    action = set_next_action(
        session, subject, "Call the party", due_date=date(2026, 8, 20),
        principal=RECORDER,
    )

    with pytest.raises(StaleNextAction):
        complete_next_action(
            session,
            subject,
            no_follow_up_reason="no_immediate_follow_up",
            expected_next_action_decision_id=action.id + 10_000,
            principal=RECORDER,
        )
    # Nothing was written: the action the coordinator saw is still current.
    assert current_next_action_decision(session, subject).id == action.id

    completed = complete_next_action(
        session,
        subject,
        no_follow_up_reason="no_immediate_follow_up",
        expected_next_action_decision_id=action.id,
        principal=RECORDER,
    )
    assert completed.predecessor_decision_id == action.id
    assert current_next_action_decision(session, subject).after_value is None


def test_a_repeated_close_cannot_close_a_later_action(session, accepted_statement):
    """The same screen submitted twice must not close a newer action (#334)."""
    subject = CoordinationSubject.statement(
        accepted_statement.commitment_lineage_id
    )
    first = set_next_action(
        session, subject, "Call the party", due_date=date(2026, 8, 20),
        principal=RECORDER,
    )
    complete_next_action(
        session,
        subject,
        no_follow_up_reason="no_immediate_follow_up",
        expected_next_action_decision_id=first.id,
        principal=RECORDER,
    )
    second = set_next_action(
        session, subject, "Confirm the revised schedule",
        due_date_unknown_reason="awaiting_external_information", principal=RECORDER,
    )

    with pytest.raises(StaleNextAction):
        complete_next_action(
            session,
            subject,
            no_follow_up_reason="no_immediate_follow_up",
            expected_next_action_decision_id=first.id,
            principal=RECORDER,
        )
    assert current_next_action_decision(session, subject).id == second.id


def test_a_permitted_successor_rejects_free_text(session, dependency):
    """A close successor must be one bounded structured choice (#334, ADR-0035)."""
    set_next_action(
        session, dependency.id, FOLLOW_UP_NEXT_ACTION_CHOICES[0],
        due_date=date(2026, 9, 1), principal=RECORDER,
    )
    with pytest.raises(CoordinationDecisionRefusal):
        complete_next_action(
            session,
            dependency.id,
            successor_action="call them again maybe",
            permitted_successor_actions=FOLLOW_UP_NEXT_ACTION_CHOICES,
            principal=RECORDER,
        )
    # The action is untouched by the refused free-text successor.
    assert (
        current_next_action_decision(session, dependency.id).after_value is not None
    )

    completed = complete_next_action(
        session,
        dependency.id,
        successor_action=FOLLOW_UP_NEXT_ACTION_CHOICES[1],
        successor_due_date=date(2026, 10, 1),
        permitted_successor_actions=FOLLOW_UP_NEXT_ACTION_CHOICES,
        principal=RECORDER,
    )
    assert completed.decision_type == "complete_next_action"
    current = current_next_action_decision(session, dependency.id)
    assert json.loads(current.after_value)["action"] == FOLLOW_UP_NEXT_ACTION_CHOICES[1]


def test_deferral_binds_to_the_action_the_coordinator_saw(
    session, accepted_statement
):
    """A stale deferral submission refuses instead of deferring a changed plan."""
    subject = CoordinationSubject.statement(
        accepted_statement.commitment_lineage_id
    )
    action = set_next_action(
        session, subject, "Call the party", due_date=date(2026, 8, 20),
        principal=RECORDER,
    )

    with pytest.raises(StaleNextAction):
        defer_work(
            session,
            subject,
            reason="waiting_for_information",
            return_date=date(2026, 9, 1),
            expected_next_action_decision_id=action.id + 10_000,
            principal=RECORDER,
        )
    assert current_deferral_decision(session, subject) is None

    deferral = defer_work(
        session,
        subject,
        reason="waiting_for_information",
        return_date=date(2026, 9, 1),
        expected_next_action_decision_id=action.id,
        principal=RECORDER,
    )
    assert deferral.deferral_return_date == date(2026, 9, 1)
    # Deferral does not close the action; it stays the current live plan.
    assert current_next_action_decision(session, subject).id == action.id


def test_changing_only_the_unknown_due_date_reason_appends_a_new_receipt(
    session, accepted_statement
):
    subject = CoordinationSubject.statement(
        accepted_statement.commitment_lineage_id
    )
    first = set_next_action(
        session,
        subject,
        "Call the party",
        due_date_unknown_reason="awaiting_external_information",
        principal=RECORDER,
    )

    second = set_next_action(
        session,
        subject,
        "Call the party",
        due_date_unknown_reason="awaiting_schedule_information",
        principal=RECORDER,
    )

    assert second.id != first.id
    assert second.predecessor_decision_id == first.id
    assert second.action_due_date_reason == "awaiting_schedule_information"
    assert current_next_action_decision(session, subject).id == second.id


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


def test_an_undated_dependency_action_projects_its_structured_reason(
    session, dependency
):
    decision = set_next_action(
        session,
        dependency.id,
        "Walk the crossing",
        due_date_unknown_reason="awaiting_external_information",
        principal=RECORDER,
    )
    session.refresh(dependency)
    assert dependency.next_action == "Walk the crossing"
    assert dependency.action_due_date is None
    assert dependency.action_due_date_reason == "awaiting_external_information"
    assert decision.action_due_date_reason == "awaiting_external_information"


def test_an_undated_action_requires_a_human_selected_reason(session, dependency):
    with pytest.raises(ValueError, match="unknown-date reason"):
        set_next_action(session, dependency.id, "Walk the crossing", principal=RECORDER)


def test_completion_clears_the_current_action_and_pins_its_predecessor(
    session, dependency
):
    first = set_next_action(
        session,
        dependency.id,
        "Walk the crossing",
        due_date_unknown_reason="awaiting_external_information",
        principal=RECORDER,
    )
    done = complete_next_action(
        session,
        dependency.id,
        no_follow_up_reason="return_condition_recorded",
        principal=RECORDER,
    )

    assert done.decision_type == "complete_next_action"
    assert done.predecessor_decision_id == first.id
    assert json.loads(done.before_value)["action"] == "Walk the crossing"
    assert done.after_value is None
    session.refresh(dependency)
    assert dependency.next_action is None
    assert dependency.action_due_date is None


def test_cancellation_is_a_distinct_decision_type(session, dependency):
    set_next_action(
        session,
        dependency.id,
        "Walk the crossing",
        due_date_unknown_reason="awaiting_external_information",
        principal=RECORDER,
    )
    withdrawn = cancel_next_action(
        session,
        dependency.id,
        no_follow_up_reason="no_immediate_follow_up",
        cancellation_reason="no_longer_needed",
        principal=RECORDER,
    )

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
        session,
        dependency.id,
        "Walk the crossing",
        due_date_unknown_reason="awaiting_external_information",
        principal=RECORDER,
    )
    done = complete_next_action(
        session,
        dependency.id,
        no_follow_up_reason="return_condition_recorded",
        principal=RECORDER,
    )
    second = set_next_action(
        session,
        dependency.id,
        "Confirm as-builts received",
        due_date_unknown_reason="awaiting_external_information",
        principal=RECORDER,
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
    set_next_action(
        session,
        dependency.id,
        "Walk the crossing",
        due_date_unknown_reason="awaiting_external_information",
        principal=RECORDER,
    )

    owner = current_internal_owner_decision(session, dependency.id)
    action = current_next_action_decision(session, dependency.id)
    assert owner is not None and owner.field == "internal_owner"
    assert action is not None and action.field == "next_action"
    assert owner.predecessor_decision_id is None
    assert action.predecessor_decision_id is None


def test_a_diverged_action_projection_refuses(session, dependency):
    set_next_action(
        session,
        dependency.id,
        "Walk the crossing",
        due_date_unknown_reason="awaiting_external_information",
        principal=RECORDER,
    )
    dependency.next_action = "Tampered"
    session.flush()

    with pytest.raises(ValueError, match="diverged"):
        set_next_action(
            session,
            dependency.id,
            "Anything else",
            due_date_unknown_reason="awaiting_external_information",
            principal=RECORDER,
        )


def test_a_blank_action_refuses(session, dependency):
    with pytest.raises(ValueError, match="Next Action"):
        set_next_action(session, dependency.id, "   ", principal=RECORDER)


def test_dependency_action_reason_projection_refuses_a_tampered_chain_tail(
    session, dependency
):
    set_next_action(
        session,
        dependency.id,
        "Walk the crossing",
        due_date_unknown_reason="awaiting_external_information",
        principal=RECORDER,
    )
    dependency.action_due_date_reason = "awaiting_schedule_information"
    session.flush()

    with pytest.raises(ValueError, match="diverged"):
        set_next_action(
            session,
            dependency.id,
            "Confirm the crossing",
            due_date_unknown_reason="awaiting_external_information",
            principal=RECORDER,
        )


def test_statement_plan_audit_trail_is_readable(session, accepted_statement):
    subject = CoordinationSubject.statement(
        accepted_statement.commitment_lineage_id
    )
    owner = assign_internal_owner(session, subject, "Dana Fields", principal=RECORDER)
    action = set_next_action(
        session,
        subject,
        "Call the party",
        due_date=date(2026, 8, 20),
        principal=RECORDER,
    )

    assert [entry.after_json for entry in trail_for_commitment_lineage(
        session, subject.commitment_lineage_id
    )] == [
        {"commitment_lineage_id": subject.commitment_lineage_id, "work_decision_id": owner.id},
        {"commitment_lineage_id": subject.commitment_lineage_id, "work_decision_id": action.id},
    ]
